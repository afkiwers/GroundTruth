"""Signal integrity on synthetic boards (analysis/si.py, the plan's M3): a board's cross-sections against closed
forms (a microstrip's Z0, a pair's Zdiff), the impedance and pair checks, the delay mismatch against the routed
lengths, crosstalk's growth with the coupled length up to saturation, a via stub's quarter wave, pair detection by
name and targets by KiCad net class."""
import json
import math

import pytest

from boardgen import line_board, microstrip_board, stub_board
from conftest import make
from groundtruth.analysis.si import (SectionCache, check_crosstalk, check_impedance, check_pairs, check_stubs,
                                     find_pairs, impedance_targets, kicad_netclasses, pair_defaults, run_si_checks)
from groundtruth.config.settings import DiffPairSpec, ImpedanceTarget, config_from_data
from groundtruth.emi.spectrum import microstrip
from groundtruth.geometry.copper import CopperMap
from groundtruth.solver.xsection import C_LIGHT


def _board(tmp_path, gen, name):
    """(board, config, cache, issues list, add) for a generated board."""
    b, sc, _ = make(tmp_path, gen, name, nets=())
    issues = []
    return b, sc.config, SectionCache(b, CopperMap(b)), issues, lambda *a, **k: issues.append((a, k))


def test_a_board_microstrip_matches_the_closed_form(tmp_path):
    """0.3 mm on 0.2 mm of FR-4 (εr 4.5) over In1's plane, 35 µm copper, no mask: Z0 within 3 % of Hammerstad with
    Wheeler's thickness correction (itself ~1 %), the same all along; εeff between 1 and εr."""
    b, cfg, cache, issues, add = _board(tmp_path, line_board(width=0.3), "ms")
    (prof,) = check_impedance(b, cache, ["SIG"], {}, 2.0, add)
    z, ee = microstrip(0.3e-3, 0.2e-3, 4.5, 35e-6)
    zs = [p[4] for p in prof.points]
    assert len(zs) == 10 and max(zs) - min(zs) < 0.01 * z
    assert zs[0] == pytest.approx(z, rel=0.03)
    assert 1 < prof.points[0][5] < 4.5 and prof.points[0][6] == pytest.approx(0.3, abs=1e-3)
    assert len(cache.solved) == 1                       # the cuts along the straight track share one solve


def test_the_solder_mask_lowers_a_microstrips_impedance_a_little(tmp_path):
    """10 µm of mask (εr 3.3) over the outer layer: Z0 down by under 3 %; no mask in the stackup: KiCad's default."""
    zs = {}
    for name, mask in (("none", 0.0), ("thin", 0.01), ("default", None)):
        b, cfg, cache, issues, add = _board(tmp_path, line_board(width=0.3, mask=mask), name)
        zs[name] = check_impedance(b, cache, ["SIG"], {}, 5.0, add)[0].points[0][4]
    assert 0.97 * zs["none"] < zs["thin"] < zs["none"]
    assert zs["default"] == pytest.approx(zs["thin"], rel=1e-6)


def test_the_net_elsewhere_in_the_cut_is_a_neighbour_not_part_of_the_line(tmp_path):
    """SIG's own track on B.Cu right under it (the net further along its route): F.Cu's Z0 stays within 1 % of the
    line alone (In1's plane shields it); driven as part of the line it read 40 % low on refboard."""
    b, cfg, cache, issues, add = _board(tmp_path, line_board(width=0.3), "alone")
    alone = check_impedance(b, cache, ["SIG"], {}, 5.0, add)[0]
    b, cfg, cache, issues, add = _board(tmp_path, line_board(width=0.3, under=True), "under")
    under = check_impedance(b, cache, ["SIG"], {}, 5.0, add)[0]
    f_cu = [p[4] for p in under.points if p[2] == "F.Cu"]
    assert f_cu and all(z == pytest.approx(alone.points[0][4], rel=0.01) for z in f_cu)


def test_an_impedance_target_flags_the_line_outside_its_tolerance(tmp_path):
    """The 0.3 mm line (~53 Ω): a 40 Ω ± 10 % target is an issue naming its length and Z0; its own Z0 + 1 Ω ± 10 %
    is not; targets by pattern."""
    b, cfg, cache, issues, add = _board(tmp_path, line_board(width=0.3), "target")
    cfg.si.impedance = [ImpedanceTarget(40.0, ["SI*"])]
    targets = impedance_targets(b, cfg)
    assert targets["SIG"][0] == 40.0 and "GND" not in targets
    check_impedance(b, cache, ["SIG"], targets, 2.0, add)
    ((args, kw),) = issues
    assert args[0] == "impedance" and "outside 40 Ω ± 10 %" in args[2] and "along 20.0 of 20.0 mm" in args[2]
    issues.clear()
    z = cache.solved[next(iter(cache.solved))].z0()
    check_impedance(b, cache, ["SIG"], {"SIG": (round(z) + 1.0, 0.1, [], "")}, 2.0, add)
    assert issues == []


def test_a_pairs_differential_impedance_and_its_coupling(tmp_path):
    """Two 0.2 mm microstrips 0.2 mm apart on 0.2 mm: Zdiff below twice the single line's Z0 (the coupling), within
    10 % of IPC-2141's 2·Z0·(1 - 0.48·exp(-0.96·s/h)); 2 mm apart: within 3 % of 2·Z0 (uncoupled)."""
    zd = {}
    for gap in (0.2, 2.0):
        b, cfg, cache, issues, add = _board(tmp_path, line_board(width=0.2, pair_gap=gap), f"pair{gap}")
        spec = DiffPairSpec("SIG_P", "SIG_N")
        (res,) = check_pairs(b, cache, [spec], 2.0, add)
        z0 = cache.solve_at((15.0, 10.0), (1.0, 0.0), "F.Cu", ["SIG_P"], 0.2)[0].z0()
        zd[gap] = (res.points[0][4], z0, res.points[0][5])
    z, z0, coupled = zd[0.2]
    assert coupled and z < 2 * z0
    assert z == pytest.approx(2 * z0 * (1 - 0.48 * math.exp(-0.96 * 0.2 / 0.2)), rel=0.10)
    z, z0, coupled = zd[2.0]
    assert z == pytest.approx(2 * z0, rel=0.03)


def test_the_pairs_skew_is_its_length_difference_at_the_lines_speed(tmp_path):
    """SIG_N 3 mm longer: the skew is 3 mm at the microstrip's speed (c/sqrt(εeff)) within 2 %; over a 10 ps limit
    an issue, under 100 ps none; a Zdiff target outside its tolerance an issue."""
    b, cfg, cache, issues, add = _board(tmp_path, line_board(width=0.2, pair_gap=0.2, n_extra=3.0), "skew")
    spec = DiffPairSpec("SIG_P", "SIG_N", zdiff=150.0, max_skew=10e-12)
    (res,) = check_pairs(b, cache, [spec], 2.0, add)
    ee = cache.solve_at((15.0, 10.4), (1.0, 0.0), "F.Cu", ["SIG_N"], 0.2)[0].eeff()        # N at y = 10.4
    assert res.skew_s == pytest.approx(3e-3 * math.sqrt(ee) / C_LIGHT, rel=0.02)
    assert {a[0] for a, _ in issues} == {"diff_skew", "diff_impedance"}
    issues.clear()
    check_pairs(b, cache, [DiffPairSpec("SIG_P", "SIG_N", max_skew=100e-12)], 2.0, add)
    assert issues == []


def test_crosstalk_grows_with_the_coupled_length_until_it_saturates(tmp_path):
    """An aggressor beside a victim: NEXT = K_b·2·T_d/t_r while the coupled stretch is shorter than half the edge,
    then K_b; FEXT grows with the length; over the limit an issue on the victim with the voltage of the swing."""
    rows = {}
    for length in (10.0, 20.0):
        b, cfg, cache, issues, add = _board(tmp_path, line_board(width=0.2, pair_gap=0.2, length=length), f"xt{length}")
        (row,) = check_crosstalk(b, cache, [("SIG_P", 1e-9, 3.3)], 0.5, add)
        rows[length] = row
        assert row["victim"] == "SIG_N" and row["coupled_mm"] == pytest.approx(length, abs=0.01) and issues == []
    short, long_ = rows[10.0], rows[20.0]
    assert long_["next"] == pytest.approx(2 * short["next"], rel=0.02)          # 2·T_d < t_r: linear
    assert long_["fext"] == pytest.approx(2 * short["fext"], rel=0.02)
    (row,) = check_crosstalk(b, cache, [("SIG_P", 10e-12, 3.3)], 0.01, add)     # a 10 ps edge: saturated
    assert row["next"] == pytest.approx(row["kb"])
    ((args, kw),) = issues
    assert args[0] == "crosstalk" and args[1] == "SIG_N" and "mV" in args[2]


def test_a_via_stub_resonates_at_its_quarter_wave(tmp_path):
    """SIG changes from F.Cu to In1.Cu through a through via: the barrel from In1 to B.Cu (1.27 mm) is a stub
    resonating at c/(4·l·sqrt(εr)) = 27.8 GHz; under 3 × the knee of a 30 ps edge (11.7 GHz) an issue, not for
    1 ns."""
    b, cfg, cache, issues, add = _board(tmp_path, stub_board(), "stub")
    (row,) = check_stubs(b, [("SIG", 1e-9)], 3.0, add)
    st = b.stackup
    stub = st.layer("B.Cu").z - st.layer("In1.Cu").z
    assert row["stub_mm"] == pytest.approx(stub) and row["layers"] == ["In1.Cu", "B.Cu"]
    assert row["f_res"] == pytest.approx(C_LIGHT / (4 * stub * 1e-3 * math.sqrt(4.5)))
    assert issues == []
    check_stubs(b, [("SIG", 30e-12)], 3.0, add)
    ((args, kw),) = issues
    assert args[0] == "via_stub" and "In1.Cu to B.Cu" in args[2]


def test_pairs_found_by_their_names(tmp_path):
    """_P/_N, CAN_H/CAN_L, USB DP/DM, D+/D-, an inverted twin RX / RX*; not two unrelated nets ending in P and N;
    the interface's defaults by name."""
    from types import SimpleNamespace
    board = SimpleNamespace(nets=["/USB/USB_DP", "/USB/USB_DM", "/COMMS/CAN_H", "/COMMS/CAN_L", "LVDS_CLK_P",
                                  "LVDS_CLK_N", "/RX", "/RX*", "D+", "D-", "VCAP", "VCAN", "GND", "TX0P", "TX0N",
                                  "CLKP", "CLKN"])
    pairs = set(find_pairs(board))
    assert {("/USB/USB_DP", "/USB/USB_DM"), ("/COMMS/CAN_H", "/COMMS/CAN_L"), ("LVDS_CLK_P", "LVDS_CLK_N"),
            ("/RX", "/RX*"), ("D+", "D-"), ("TX0P", "TX0N"), ("CLKP", "CLKN")} <= pairs
    assert not any("VCAP" in p for p in pairs)
    assert pair_defaults("/USB/USB_DP", "/USB/USB_DM")[:1] == (90.0,)
    assert pair_defaults("/COMMS/CAN_H", "/COMMS/CAN_L") == (None, 50e-9, "CAN")


def test_targets_by_kicad_net_class(tmp_path):
    """A net class from the project file beside the board (patterns with wildcards, explicit assignments)."""
    b, sc, path = make(tmp_path, line_board(width=0.3, pair_gap=0.3), "cls", nets=())
    path.with_suffix(".kicad_pro").write_text(json.dumps({"net_settings": {
        "classes": [{"name": "Default"}, {"name": "HS"}],
        "netclass_patterns": [{"netclass": "HS", "pattern": "SIG_*"}],
        "netclass_assignments": {"SIG_N": "Slow"}}}), encoding="utf-8")
    assert kicad_netclasses(b) == {"SIG_P": "HS", "SIG_N": "Slow"}
    cfg = sc.config
    cfg.si.impedance = [ImpedanceTarget(45.0, netclass="HS")]
    assert impedance_targets(b, cfg) == {"SIG_P": (45.0, 0.1, [], "net class HS")}


def test_run_checks_profiles_the_listed_nets(tmp_path):
    """Run checks on the microstrip board with SIG listed: its Z0 profile in the result (no target: no issue)."""
    from groundtruth.rules.checks import run_checks
    b, sc, _ = make(tmp_path, microstrip_board(layers=4), "listed")
    res = run_checks(b, sc.config)
    (prof,) = [n for n in res.si.nets if n.net == "SIG"]
    assert prof.points and prof.target is None and res.si.sections >= 1
    assert not [i for i in res.issues if i.check == "impedance"]


def test_the_si_settings():
    """si: impedance targets, pairs with units and defaults; a target without nets refused."""
    from groundtruth.config.settings import ConfigError
    si = config_from_data({"si": {"impedance": [{"nets": "A, B*", "z0": "50Ω", "tol": "5%", "layers": ["F.Cu"]}],
                                  "pairs": [{"p": "X_P", "n": "X_N", "zdiff": "90Ω", "rise_time": "200ps"}],
                                  "crosstalk_max": "3%", "stub_factor": 5, "step_mm": "1mm"}}).si
    (t,) = si.impedance
    assert t.nets == ["A", "B*"] and t.z0 == 50.0 and t.tol == pytest.approx(0.05) and t.layers == ["F.Cu"]
    (p,) = si.pairs
    assert p.zdiff == 90.0 and p.skew_limit == pytest.approx(20e-12) and p.label == "X_P / X_N"
    assert si.crosstalk_max == pytest.approx(0.03) and si.stub_factor == 5 and si.step_mm == 1.0
    with pytest.raises(ConfigError, match="give its nets"):
        config_from_data({"si": {"impedance": [{"z0": 50}]}})


def _profile_project(path, z0=0.0, kind=0, delay_as_mm=10000000, cls="HS", pattern="SIG*"):
    """A project file beside the board as KiCad 10 writes a tuning profile (the user's "Test" profile on
    refboard: type 0 single, the unit delay in attoseconds per mm, widths in nm), its class on `pattern`."""
    path.with_suffix(".kicad_pro").write_text(json.dumps({
        "net_settings": {"classes": [{"name": "Default", "tuning_profile": ""}, {"name": cls, "tuning_profile": "Test"}],
                         "netclass_patterns": [{"netclass": cls, "pattern": pattern}]},
        "tuning_profiles": {"meta": {"version": 0}, "tuning_profiles_impedance_geometric": [{
            "enable_time_domain_tuning": True, "profile_name": "Test", "target_impedance": z0, "type": kind,
            "layer_entries": [{"bottom_reference_layer": "In1.Cu", "delay": delay_as_mm, "diff_pair_gap": 200000,
                               "signal_layer": "F.Cu", "top_reference_layer": "UNDEFINED", "width": 300000}],
            "via_overrides": [], "via_prop_delay": 0}]}}), encoding="utf-8")


def test_a_kicad_tuning_profile_is_read(tmp_path):
    """The profile's unit delay 10 000 000 as/mm is 100 ps/cm (1e-8 s/m), widths in nm, "UNDEFINED" no layer; its
    class's nets are its nets; its target impedance is their Z0 target."""
    from groundtruth.analysis.si import kicad_profiles, profile_nets
    b, sc, path = make(tmp_path, line_board(width=0.3), "prof", nets=())
    _profile_project(path, z0=45.0)
    (name, p), = kicad_profiles(b).items()
    entry = p["layers"]["F.Cu"]
    assert name == "Test" and p["z0"] == 45.0 and not p["diff"] and p["time_domain"] and p["classes"] == ["HS"]
    assert entry["delay"] == pytest.approx(1e-8) and entry["width_mm"] == pytest.approx(0.3)
    assert entry["top"] is None and entry["bottom"] == "In1.Cu"
    assert profile_nets(b) == {"SIG": "Test"}
    assert impedance_targets(b, sc.config)["SIG"] == (45.0, 0.1, ["F.Cu"], "KiCad tuning profile Test")


def test_a_profiles_unit_delay_against_the_cross_sections(tmp_path):
    """KiCad tunes lengths to a delay with the profile's unit delay: 100 ps/cm against the microstrip's
    sqrt(εeff)/c (about 61 ps/cm) is a tuning_delay issue naming the solved figure; the solved figure entered is
    none. A profile on KiCad's Default class applies to the listed nets only (not every net of the board)."""
    from groundtruth.rules.checks import run_checks
    b, sc, path = make(tmp_path, line_board(width=0.3), "delay", nets=())
    _profile_project(path)
    res = run_checks(b, sc.config)
    (row,) = res.si.delays
    ee = res.si.nets[0].points[0][5]
    assert row["solved_ps_cm"] == pytest.approx(math.sqrt(ee) / C_LIGHT * 1e10, rel=1e-3)
    assert row["kicad_ps_cm"] == pytest.approx(100.0) and 55 < row["solved_ps_cm"] < 66
    (i,) = [i for i in res.issues if i.check == "tuning_delay"]
    assert f"{row['solved_ps_cm']:.1f} ps/cm" in i.message and i.layer == "F.Cu"
    _profile_project(path, delay_as_mm=round(row["solved_ps_cm"] * 1e5))       # ps/cm -> as/mm
    assert not [i for i in run_checks(b, sc.config).issues if i.check == "tuning_delay"]
    _profile_project(path, z0=45.0, cls="Default", pattern="nothing")          # every net is in Default
    assert impedance_targets(b, sc.config) == {} and run_checks(b, sc.config).si.nets == []


def test_a_differential_profile_gives_a_pair_its_target(tmp_path):
    """type 1 (Differential): a listed pair without its own Zdiff takes the profile's target impedance."""
    b, sc, path = make(tmp_path, line_board(width=0.2, pair_gap=0.2), "dprof", nets=())
    _profile_project(path, z0=150.0, kind=1, pattern="SIG_*")
    cfg = sc.config
    cfg.si.pairs = [DiffPairSpec("SIG_P", "SIG_N")]
    issues = []
    res = run_si_checks(b, cfg, CopperMap(b), lambda *a, **k: issues.append(a))
    assert res.pairs[0].spec.zdiff == 150.0 and "tuning profile Test" in res.pairs[0].spec.note
    assert any(a[0] == "diff_impedance" for a in issues)
