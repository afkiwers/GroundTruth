"""DC rails with loads (analysis/dcload.py), against analytic results: the drop along a pour is I·R with
R = L / (σ·w·t), the solve is linear (several loads = the sum of each alone), copper past the last load carries
nothing, the ground return adds its own drop, and the settings round-trip."""
import pytest

from boardgen import BoardGen
from conftest import make
from groundtruth.analysis.simulate import simulate
from groundtruth.config.settings import DcLoad, DcLoadSpec, SettingsFile
from groundtruth.solver.peec import SIGMA_CU
from test_pours import WIDTH, X0, Y, pour_board


def board_with(tmp_path, length, loads, name, ret=False, mid=False):
    """test_pours' 4 mm PWR pour (U1 at one end, U2 at the other, U3 in the middle when `mid`), with one DC rail fed
    at U1.1: `loads` = [(pin, A)]; `ret`: each load returns through its GND pin (pad 2) to U1.2."""
    g = pour_board(length)
    if mid:
        x = X0 + length / 2
        g.footprint("U3", "IC", "Package_SO:Fake_IC", (x, Y), [
            {"number": 1, "at": (0, 0), "net": "PWR", "size": (1.0, WIDTH - 0.4)},
            {"number": 2, "at": (0, WIDTH / 2 + 1.5), "net": "GND", "size": (1.0, 1.0)}])
        g.via((x, Y + WIDTH / 2 + 1.5), "GND", ("F.Cu", "B.Cu"))
    board, sc, _ = make(tmp_path, g, name, nets=())
    spec = DcLoadSpec("rail", "U1.1", [DcLoad(p, a, p.split(".")[0] + ".2" if ret else "") for p, a in loads],
                      "U1.2" if ret else "")
    sc.set_dc_loads([spec])
    sc.save()
    return board, SettingsFile.load(sc.path).config


def drops(res) -> dict:
    """pin -> (supply, return, total) drop in mV."""
    return {r["pin"]: (r["supply_mv"], r["return_mv"], r["drop_mv"]) for r in res.loads}


def test_the_drop_along_a_pour_is_its_resistance_times_the_current(tmp_path):
    """One 2 A load at the far end of a 4 mm × 35 µm pour: 20 mm more pour adds 2 A × 20 mm / (σ·w·t) of drop (end
    effects cancel in the difference); the loss is what the copper dissipates (drop x current, supply side only).
    Converged at 4x (cells 0.12 mm: +0.1 %, also at 16x); 1x reads 5.5 % low (0.24 mm cells widen the pour's edges)."""
    from dataclasses import replace
    exact = 1 / (SIGMA_CU * WIDTH * 1e-3 * 35e-6)
    for resolution, tol in ((4.0, 0.02), (1.0, 0.07)):
        out = {}
        for length in (20.0, 40.0):
            board, cfg = board_with(tmp_path, length, [("U2.1", 2.0)], f"r{int(length)}_{int(resolution)}")
            cfg = replace(cfg, analysis=replace(cfg.analysis, resolution=resolution))
            res = simulate(board, cfg)
            assert not res.dc_loads[0].error, res.dc_loads[0].error
            out[length] = res.dc_loads[0]
        d20, d40 = drops(out[20.0])["U2.1"][0], drops(out[40.0])["U2.1"][0]
        per_m = (d40 - d20) * 1e-3 / 2.0 / 0.020
        assert per_m == pytest.approx(exact, rel=tol)
    p = out[40.0].dc[0]
    assert p["loss_w"] == pytest.approx(d40 * 1e-3 * 2.0, rel=0.02)
    assert out[40.0].sheet_current and out[40.0].layer_grids and out[40.0].kind == "dcload"


def test_loads_add_up_and_copper_past_the_last_load_carries_nothing(tmp_path):
    """A load in the middle alone: the far pad sits at the middle pad's potential (nothing flows past it). Both
    loads: each pad's drop is the sum of what each load alone causes (the solve is linear)."""
    board, cfg = board_with(tmp_path, 40.0, [("U3.1", 1.0), ("U2.1", 1.0)], "both", mid=True)
    both = drops(simulate(board, cfg).dc_loads[0])
    board, cfg = board_with(tmp_path, 40.0, [("U3.1", 1.0)], "mid", mid=True)
    mid = drops(simulate(board, cfg).dc_loads[0])["U3.1"][0]
    board, cfg = board_with(tmp_path, 40.0, [("U2.1", 1.0)], "far", mid=True)
    far = simulate(board, cfg).dc_loads[0]
    far_at = drops(far)["U2.1"][0]
    # the far load alone, read at the middle pad: half the pour's resistance (from the potentials of its result)
    assert both["U2.1"][0] > both["U3.1"][0] > 0
    assert both["U3.1"][0] == pytest.approx(mid + far_at / 2, rel=0.06)
    assert both["U2.1"][0] == pytest.approx(far_at + mid, rel=0.06)


def test_the_ground_return_adds_its_own_drop(tmp_path):
    """With the returns (each load's GND pin and the source's) the ground plane's drop is added: small (a plane)
    but positive, and the supply side is unchanged."""
    board, cfg = board_with(tmp_path, 40.0, [("U2.1", 2.0)], "ret", ret=True)
    res = simulate(board, cfg).dc_loads[0]
    sup, back, total = drops(res)["U2.1"]
    board, cfg = board_with(tmp_path, 40.0, [("U2.1", 2.0)], "noret")
    sup0 = drops(simulate(board, cfg).dc_loads[0])["U2.1"][0]
    assert back > 0 and total == pytest.approx(sup + back) and back < sup
    assert sup == pytest.approx(sup0, rel=0.02)


def test_dc_loads_round_trip_through_the_settings_file(tmp_path):
    """Written and read again: the same rails; a load also reads as "PIN/RETURN=CURRENT"."""
    board, cfg = board_with(tmp_path, 20.0, [("U2.1", 0.5)], "rt", ret=True)
    d = cfg.dc_loads[0]
    assert (d.source, d.source_return, d.loads[0].pin, d.loads[0].ret, d.loads[0].current) == \
        ("U1.1", "U1.2", "U2.1", "U2.2", 0.5)


def test_a_rail_heats_the_board_and_its_capacity_follows_the_square_root_law(tmp_path, monkeypatch):
    """Thermal and Current capacity take a DC rail as they take a power stage's DC paths. Linear thermal problem
    (copper α = 0, fixed films, no radiation): the rise is ∝ I², so the current for 4x the rise is 2x the rail's."""
    pytest.importorskip("pyamg")
    pytest.importorskip("numba")
    from dataclasses import replace
    from groundtruth.analysis.thermal import run_capacity, run_thermal
    from groundtruth.thermal import materials
    # the solver's modules bind COPPER when first imported: import them before the patch, or they keep the
    # patched copper for the rest of the test run (PCB Thermal's own model in test_thermal then read 6.7 % off)
    import groundtruth.thermal.solver.coupled  # noqa: F401
    import groundtruth.thermal.solver.electrical  # noqa: F401
    import groundtruth.thermal.solver.stack  # noqa: F401
    import groundtruth.thermal.solver.thermal  # noqa: F401
    monkeypatch.setattr(materials, "COPPER", replace(materials.COPPER, alpha=0.0))
    board, cfg = board_with(tmp_path, 40.0, [("U2.1", 6.0)], "hot")
    cfg = replace(cfg, thermal=replace(cfg.thermal, quality="draft", convection="fixed", radiation=False))
    rail = simulate(board, cfg).dc_loads[0]
    nominal, _ = run_thermal(board, cfg, [rail])
    assert not nominal.error, nominal.error
    rise = nominal.t_max - cfg.thermal.ambient_c
    assert rise > 2
    cfg = replace(cfg, thermal=replace(cfg.thermal, capacity_c=cfg.thermal.ambient_c + 4 * rise))
    (view,) = run_capacity(board, cfg, [rail])
    c = view.capacity
    assert not view.error and c["stage"] == "rail" and c["i_nominal"] == pytest.approx(6.0)
    assert c["i_capacity"] == pytest.approx(12.0, rel=0.01), c


def test_the_report_and_the_console_show_each_load_s_drop(tmp_path):
    """The HTML report's DC loads part (its loads table and map) and the console's lines."""
    from groundtruth.report import formats
    from groundtruth.report.html import render_html
    board, cfg = board_with(tmp_path, 40.0, [("U3.1", 1.0), ("U2.1", 1.0)], "rep", mid=True)
    res = simulate(board, cfg)
    page = render_html(board, res.issues, res.routes, "simulation", config=cfg, dc_loads=res.dc_loads)
    assert "DC loads" in page and "U2.1" in page and "Total drop" in page and page.count("<img") >= 1
    text = formats.to_text(res.issues, [], [], [], res.dc_loads)
    assert "DC load rail: 2 A from U1.1" in text and "U3.1: 1 A, drop" in text


def test_fill_from_net_finds_the_rail_s_parts_and_their_ground_pins(tmp_path):
    """The DC loads tab's helper on a distribution board: every part on VDD with its ground pin, the connectors first
    (J2 before J10), the capacitors after; the 2-pin connector is the guessed source."""
    from boardgen import distribution_board
    from groundtruth.analysis.dcload import guess_source, rail_candidates
    board, _, _ = make(tmp_path, distribution_board(), "dist", nets=())
    cands = rail_candidates(board, "VDD", "GND")
    assert [c["ref"] for c in cands] == ["J1", "J2", "J3", "J4", "J5", "C1", "C2", "C3", "C4"]
    assert cands[0]["pin"] == "J1.2" and cands[0]["ret"] == "J1.1" and cands[1]["ret"] == "J2.1"
    assert cands[guess_source(cands)]["ref"] == "J1"
    assert rail_candidates(board, "VDD")[1]["ret"] == ""                # no ground net asked: no returns


def test_a_distribution_rail_s_drop_grows_along_it(tmp_path):
    """The distribution board's rail fed at J1, 0.5 A at each of J2-J5 with their returns: every load sees a drop, the
    farther the more; the busiest copper is next to the source, where all 2 A flow."""
    from boardgen import distribution_board
    board, sc, _ = make(tmp_path, distribution_board(), "dist2", nets=())
    sc.set_dc_loads([DcLoadSpec("VDD rail", "J1.2", [DcLoad(f"J{k}.2", 0.5, f"J{k}.1") for k in (2, 3, 4, 5)],
                                "J1.1")])
    sc.save()
    res = simulate(board, SettingsFile.load(sc.path).config).dc_loads[0]
    assert not res.error, res.error
    d = [r["drop_mv"] for r in res.loads]
    assert all(x > 0 for x in d) and d == sorted(d)
    assert res.dc[0]["sustained_at"][0] < 33.0


def test_the_form_offers_the_pins_that_fit(tmp_path):
    """The DC loads form completes pins (the user: "can you auto complete the source pin source return as well as the
    loads?"): the source offers supply connectors first, the return the source part's own ground pin, a load the pins
    on the source's net (not the source, not the loads already listed), a load's return its own part's pin on the
    return net; typing narrows by the pin's start or (2+ letters) its net; nothing in the current."""
    from boardgen import distribution_board
    from groundtruth.ui.pin_complete import complete_field, complete_load_line, offer, pin_table
    board, _, _ = make(tmp_path, distribution_board(), "dist", nets=())
    pins = pin_table(board)
    assert pins[0].pin == "C1.1" and "J10.1" not in [p.pin for p in pins]
    first = [p.pin for p in offer(pins, "source", "")]
    assert first[:5] == ["J1.2", "J2.2", "J3.2", "J4.2", "J5.2"] and first[-1].endswith(".1") and "GND" in \
        next(p.net for p in pins if p.pin == first[-1])
    assert [p.pin for p in offer(pins, "source", "j3")][:1] == ["J3.2"]
    assert {p.net for p in offer(pins, "source", "can")} == {"CANH", "CANL"}
    c = complete_field(pins, "source_return", "", 0, source="J1.2")
    assert c.pins[0].pin == "J1.1" and c.start == 0 and c.end == 0
    assert complete_field(pins, "source", "J1.2", 4) is None              # typed in full: nothing to offer
    c = complete_load_line(pins, "", 0, "J1.2", "J1.1", listed=("J2.2",))
    assert [p.pin for p in c.pins][:3] == ["J3.2", "J4.2", "J5.2"] and c.suffix == "/"
    assert all(p.net == "VDD" for p in c.pins[:7]) and "J1.2" not in [p.pin for p in c.pins]
    assert complete_load_line(pins, "", 0, "J1.2", "").suffix == "="       # no return given: straight to the current
    c = complete_load_line(pins, "J4.2/", 5, "J1.2", "J1.1")
    assert c.pins[0].pin == "J4.1" and (c.start, c.end) == (5, 5) and c.suffix == "="
    c = complete_load_line(pins, "J4.2/J=1A", 6, "J1.2", "J1.1")
    assert c.pins[0].pin == "J4.1" and (c.start, c.end) == (5, 6) and c.suffix == ""
    assert complete_load_line(pins, "J4.2/J4.1=0.", 12, "J1.2", "J1.1") is None


def _sensor_return_board(tmp_path):
    """A rail from J1.2 to J2.2 on PWR whose return runs from J2.1 (RET) through a Hall current sensor's primary
    (U1: IP+ on RET, IP- on GND) to J1.1 (GND), as on a battery monitor board."""
    g = BoardGen(layers=2, size=(40.0, 20.0))
    for ref, x in (("J1", 5.0), ("J2", 35.0)):
        g.footprint(ref, "CONN", "Connector:Fake_2", (x, 10.0), [
            {"number": 1, "at": (0, 3.0), "net": "GND" if ref == "J1" else "RET", "size": (2.0, 2.0)},
            {"number": 2, "at": (0, 0), "net": "PWR", "size": (2.0, 2.0)}])
    g.footprint("U1", "ACS772", "Sensor_Current:Fake_CB", (20.0, 13.0), [
        {"number": 1, "at": (0, -2.0), "net": "+5V", "pintype": "power_in", "pinfunction": "VCC"},
        {"number": 4, "at": (2.0, 0), "net": "RET", "pintype": "passive", "pinfunction": "IP+", "size": (1.5, 1.5)},
        {"number": 5, "at": (-2.0, 0), "net": "GND", "pintype": "passive", "pinfunction": "IP-", "size": (1.5, 1.5)}])
    pwr = g.track((5.0, 10.0), (35.0, 10.0), "PWR", width=2.0)
    g.track((35.0, 13.0), (22.0, 13.0), "RET", width=2.0)
    g.track((18.0, 13.0), (5.0, 13.0), "GND", width=2.0)
    board, sc, _ = make(tmp_path, g, "sensor", nets=())
    sc.set_dc_loads([DcLoadSpec("rail", "J1.2", [DcLoad("J2.2", 10.0, "J2.1")], "J1.1")])
    sc.save()
    return board, SettingsFile.load(sc.path).config, pwr


def test_a_return_through_a_sensor_without_a_model_names_it_and_what_to_set(tmp_path):
    """The rail's return runs through a current sensor's primary whose model passes no current: Simulate's error names
    the part, the two nets it joins and the pins to give a resistor model; with that model (1 mOhm on IP+ / IP-)
    there is nothing to name and the return current flows through it: the return side drops more than its I·R."""
    from groundtruth.analysis.dcload import open_links
    from groundtruth.components.models import ComponentModel
    board, cfg, _ = _sensor_return_board(tmp_path)
    cfg.components["U1"].model = ComponentModel("open")
    text = open_links(board, cfg, cfg.dc_loads[0])
    assert "U1 joins GND and RET" in text and "pins IP+, IP-" in text, text
    assert "U1 joins GND and RET" in simulate(board, cfg).dc_loads[0].error
    cfg.components["U1"].model = ComponentModel("resistor", {"R": 1e-3}, pins=["IP+", "IP-"])
    assert open_links(board, cfg, cfg.dc_loads[0]) == ""
    # solved through the primary now: the return side drops at least its 10 A x 1 mOhm, plus its copper's
    res = simulate(board, cfg).dc_loads[0]
    assert not res.error and res.loads[0]["return_mv"] > 10.0, (res.error, res.loads)


def test_a_dc_rail_picked_in_its_tab_lights_the_copper_its_current_runs_on(tmp_path):
    """DC loads tab, a row clicked (analysis/highlight.dcloads_highlight): its pins' pads and the supply and the
    non-reference return copper are selected and lit, the reference net's copper only in the zoom; a part whose model
    joins two of its nets (the sensor's primary) is selected too."""
    from groundtruth.analysis.highlight import dcloads_highlight
    from groundtruth.components.models import ComponentModel
    board, cfg, pwr = _sensor_return_board(tmp_path)
    cfg.components["U1"].model = ComponentModel("resistor", {"R": 100e-6}, pins=["IP+", "IP-"])
    h = dcloads_highlight(["rail"], board, cfg)
    gnd = [t.uuid for t in board.tracks if t.net == "GND"]
    ret = [t.uuid for t in board.tracks if t.net == "RET"]
    assert pwr in h.lit and ret[0] in h.lit and not set(gnd) & set(h.lit) and set(gnd) <= set(h.context)
    assert board.footprint("U1").uuid in h.focus and "through U1" in h.text
    assert dcloads_highlight(["nothing"], board, cfg).empty


def test_dc_rails_alone_together_give_each_via_a_share_of_their_currents(tmp_path):
    """Two DC rails and no power stage: the combined view (power.combine_dc) takes its current in from the rails'
    own currents (there is no input path), so each via's share is of that total and at most 100 %. The user, of a
    map whose vias read "80307840008252 %": the shares had been divided by the stages' 0 A input (1e-12)."""
    from groundtruth.analysis.power import COMBINED_VIEW, dc_view
    board, cfg = board_with(tmp_path, 30.0, [("U2.1", 2.0)], "two", ret=True)
    second = DcLoadSpec("ground", "U1.2", [DcLoad("U2.2", 3.0, "")], "")
    sf = SettingsFile.load(next((tmp_path).rglob("two*.groundtruth.yaml")))
    sf.set_dc_loads([cfg.dc_loads[0], second])
    sf.save()
    res = simulate(board, SettingsFile.load(sf.path).config)
    assert all(not r.error for r in res.dc_loads), [r.error for r in res.dc_loads]
    combo = res.dc_combined
    assert combo is not None
    path = combo.dc[0]
    assert path["current_a"] == pytest.approx(5.0) and "5 A in" in dc_view(combo, COMBINED_VIEW).heat_title
    shares = [t["fraction"] for t in dc_view(combo, COMBINED_VIEW).transfers]
    assert shares and all(0 < s <= 1.0 + 1e-6 for s in shares)
